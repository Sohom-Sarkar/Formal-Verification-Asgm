// Reference: parameterised ripple-carry adder.
module addN #(parameter WIDTH = 16)
             (input [WIDTH-1:0] a, input [WIDTH-1:0] b, input cin,
              output [WIDTH-1:0] sum, output cout);
  wire [WIDTH:0] c;
  assign c[0] = cin;

  genvar i;
  generate
    for (i = 0; i < WIDTH; i = i + 1) begin : stage
      assign sum[i] = a[i] ^ b[i] ^ c[i];
      assign c[i+1] = (a[i] & b[i]) | (c[i] & (a[i] ^ b[i]));
    end
  endgenerate

  assign cout = c[WIDTH];
endmodule
