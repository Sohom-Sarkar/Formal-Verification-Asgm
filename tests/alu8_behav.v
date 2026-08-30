// Reference: 8-bit ALU written as a behavioural case statement.
module alu8(input [7:0] a, input [7:0] b, input [2:0] op,
            output reg [7:0] y, output reg zero);
  always @(*) begin
    case (op)
      3'd0: y = a + b;
      3'd1: y = a - b;
      3'd2: y = a & b;
      3'd3: y = a | b;
      3'd4: y = a ^ b;
      3'd5: y = a << b[2:0];
      3'd6: y = a >> b[2:0];
      3'd7: y = {7'd0, (a < b)};
      default: y = 8'd0;
    endcase
    zero = (y == 8'd0);
  end
endmodule
