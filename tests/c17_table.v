// ISCAS-85 c17 restated as an exhaustive truth table.
// Functionally identical to the six-NAND netlist, but with no
// structural resemblance to it at all - a 32-entry lookup versus
// six gates. Generated from the published c17 function.
module c17(input N1, input N2, input N3, input N6, input N7,
           output reg N22, output reg N23);
  always @(*) begin
    case ({N1, N2, N3, N6, N7})
      5'b00000: begin N22 = 1'b0; N23 = 1'b0; end
      5'b00001: begin N22 = 1'b0; N23 = 1'b1; end
      5'b00010: begin N22 = 1'b0; N23 = 1'b0; end
      5'b00011: begin N22 = 1'b0; N23 = 1'b1; end
      5'b00100: begin N22 = 1'b0; N23 = 1'b0; end
      5'b00101: begin N22 = 1'b0; N23 = 1'b1; end
      5'b00110: begin N22 = 1'b0; N23 = 1'b0; end
      5'b00111: begin N22 = 1'b0; N23 = 1'b0; end
      5'b01000: begin N22 = 1'b1; N23 = 1'b1; end
      5'b01001: begin N22 = 1'b1; N23 = 1'b1; end
      5'b01010: begin N22 = 1'b1; N23 = 1'b1; end
      5'b01011: begin N22 = 1'b1; N23 = 1'b1; end
      5'b01100: begin N22 = 1'b1; N23 = 1'b1; end
      5'b01101: begin N22 = 1'b1; N23 = 1'b1; end
      5'b01110: begin N22 = 1'b0; N23 = 1'b0; end
      5'b01111: begin N22 = 1'b0; N23 = 1'b0; end
      5'b10000: begin N22 = 1'b0; N23 = 1'b0; end
      5'b10001: begin N22 = 1'b0; N23 = 1'b1; end
      5'b10010: begin N22 = 1'b0; N23 = 1'b0; end
      5'b10011: begin N22 = 1'b0; N23 = 1'b1; end
      5'b10100: begin N22 = 1'b1; N23 = 1'b0; end
      5'b10101: begin N22 = 1'b1; N23 = 1'b1; end
      5'b10110: begin N22 = 1'b1; N23 = 1'b0; end
      5'b10111: begin N22 = 1'b1; N23 = 1'b0; end
      5'b11000: begin N22 = 1'b1; N23 = 1'b1; end
      5'b11001: begin N22 = 1'b1; N23 = 1'b1; end
      5'b11010: begin N22 = 1'b1; N23 = 1'b1; end
      5'b11011: begin N22 = 1'b1; N23 = 1'b1; end
      5'b11100: begin N22 = 1'b1; N23 = 1'b1; end
      5'b11101: begin N22 = 1'b1; N23 = 1'b1; end
      5'b11110: begin N22 = 1'b1; N23 = 1'b0; end
      5'b11111: begin N22 = 1'b1; N23 = 1'b0; end
      default: begin N22 = 1'b0; N23 = 1'b0; end
    endcase
  end
endmodule
